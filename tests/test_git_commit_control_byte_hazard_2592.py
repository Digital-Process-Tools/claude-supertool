"""#2592 -- a raw control byte in MESSAGE is refused before staging, not
left to surface later as a misleading "no PATHS were given".

On lane fix/2573, `git-commit:::MESSAGE:::PATH` refused with `no PATHS were
given` on the first attempt at both commits, despite a PATH given inline.
One of the two actual commit messages (51cad016) carries a real ESC (0x1B)
control byte embedded in prose that quotes a Python string literal
(a Python \x1b escape) -- confirmed by reading the commit back with `git
show` and inspecting repr() of the bytes, not by guessing from the markdown
source.

Fed straight through this repo's own argv-list parser (no shell involved,
matching `_run` below), that exact message-plus-path combination commits
cleanly -- so the tokenizer itself is not at fault, and this is not re-tested
here (see test_git_commit_cause_and_all_1155_1137.py's own multi-line-message
coverage). The leading theory for where the PATH actually vanished is a shell
quoting form ($'...') turning \x1b TEXT into this real byte, then an
interactive pty's own readline reading the byte as a control sequence and
dropping part of the line before python's argv ever forms -- outside this
script's own reach once it happens.

What IS in this script's reach: refusing the byte outright, the moment a call
does carry it whole, so the mistake is caught before a caller with a
hazardous byte still in hand goes anywhere near a shell a second time.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

_COMMIT_PATH = REPO / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_2592", _COMMIT_PATH)
assert _spec is not None and _spec.loader is not None
commit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit_mod)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n',
                                          encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list, cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=dict(os.environ),
    )
    return proc.stdout + proc.stderr


def _head_subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


# --- unit level: the hazard predicate itself -------------------------------


def test_control_byte_hazard_flags_a_real_esc_byte() -> None:
    hazard = commit_mod._control_byte_hazard("before" + chr(0x1B) + "after")
    assert hazard is not None
    index, display, kind = hazard
    assert index == 6
    assert kind == "control byte"


def test_control_byte_hazard_is_none_for_the_actual_2573_message() -> None:
    """Positive control: ordinary `\n`/`\r`/`\t` in a real multi-paragraph
    message must not trip this -- only a byte with no business being there."""
    msg = "line one\n\nline two\twith a tab\r\n"
    assert commit_mod._control_byte_hazard(msg) is None


# --- review finding 2: U+2028/U+2029 are labelled, not called "control byte" ---


def test_line_separator_is_labelled_as_such_not_a_control_byte() -> None:
    hazard = commit_mod._control_byte_hazard("before" + chr(0x2028) + "after")
    assert hazard is not None
    _, _, kind = hazard
    assert kind == "line separator"


def test_paragraph_separator_is_labelled_as_such_not_a_control_byte() -> None:
    hazard = commit_mod._control_byte_hazard("before" + chr(0x2029) + "after")
    assert hazard is not None
    _, _, kind = hazard
    assert kind == "paragraph separator"


# --- review finding 1: a real C1 byte, in the ONLY form it actually --------
# --- arrives through a CLI call (a lone surrogate, per surrogateescape) ----


def test_surrogateescaped_c1_byte_is_flagged_by_its_original_value() -> None:
    """A standalone C1 byte (e.g. NEL, 0x85) is not valid UTF-8 alone, so a
    real CLI call never hands this script `chr(0x85)` -- it hands the lone
    surrogate `surrogateescape` produces instead. The predicate must catch
    THAT shape, not the shape no real call can ever deliver."""
    surrogate_for_0x85 = chr(0xDC00 + 0x85)
    hazard = commit_mod._control_byte_hazard("before" + surrogate_for_0x85
                                              + "after")
    assert hazard is not None
    index, display, kind = hazard
    assert index == 6
    assert kind == "control byte"
    assert "0x85" in display


def test_ordinary_invalid_utf8_surrogate_outside_c1_range_is_not_flagged() -> None:
    """Positive control: `surrogateescape` uses the WHOLE 0xDC80-0xDCFF
    window for every byte it cannot decode, not only C1 (0x80-0x9F) -- a
    byte outside that narrower range must not be misreported as a control
    byte."""
    surrogate_for_0xC0 = chr(0xDC00 + 0xC0)
    assert commit_mod._control_byte_hazard(surrogate_for_0xC0) is None


def test_surrogateescaped_c1_byte_is_refused_through_the_real_cli_path(
        tmp_path: Path) -> None:
    """The reachability itself, not just the predicate: a genuine raw 0x85
    byte handed to the OS as real argv bytes (bypassing this test runner's
    own str encoding, the same hop a live shell/exec makes) must still be
    caught end to end -- this is what review finding 1 said was
    unreachable before `_surrogateescaped_c1_byte` closed it."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    op_bytes = b"git-commit:::before\x85after:::a.txt"

    proc = subprocess.run(
        [os.fsencode(sys.executable), os.fsencode(str(SUPERTOOL)), op_bytes],
        capture_output=True, timeout=120, cwd=str(work), env=dict(os.environ),
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")

    assert "ERROR" in out, out
    assert "control byte" in out, out
    assert _head_subject(work) == "seed"


# --- end-to-end: the colon-CLI route ---------------------------------------


def test_esc_byte_in_message_is_refused_before_staging(tmp_path: Path) -> None:
    """The #2592 shape: MESSAGE carries a raw ESC and a PATH is named right
    after it. This must be refused up front, never silently misreported as
    `no PATHS were given` further down the same call."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    msg = "subject" + chr(0x1B) + "?tail"

    out = _run(["git-commit:::" + msg + ":::a.txt"], cwd=work)

    assert "ERROR" in out, out
    assert "control byte" in out, out
    assert "no PATHS" not in out, out
    assert _head_subject(work) == "seed"


def test_ordinary_multiline_message_with_inline_path_is_unaffected(
        tmp_path: Path) -> None:
    """Positive control for the end-to-end path: a real multi-paragraph
    message with no hazardous byte must commit exactly as it always has."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    msg = "subject line\n\nbody line one\nbody line two"

    out = _run(["git-commit:::" + msg + ":::a.txt"], cwd=work)

    assert "ERROR" not in out, out
    assert _head_subject(work) == "subject line"
