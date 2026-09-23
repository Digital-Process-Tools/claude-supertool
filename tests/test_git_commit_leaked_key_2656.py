"""#2656 -- `git-commit` must refuse a subject that leaked a payload key.

Observed 2026-09-19, commit `112e504` in `claude-swarm-builder`: a well-formed
`git-commit:@-` payload of the shape

    message = '''doctor: scaffold owned files, ...'''
    paths = ["--all"]

produced a subject of `paths = ["--all"] doctor: scaffold owned files, ...`
-- the `paths` line prepended to the message. The exact bytes that produced
it were not recovered (a spawned `oss:doctor` agent inside `claude -p`, not
in hand), and re-sending the same payload here does not reproduce it: the
payload parser's own `'''` early-close case already refuses loudly before
commit.py ever runs (#1830, #1868 -- `_toml_multiline_close`'s own leftover
detection catches an odd OR even count of embedded `'''` runs and refuses
with "a ''' run inside a value closed the block early", proven below).

So this is deliberately NOT a reproduction of the leak's root cause -- the
issue's own "What I could not pin down" section says the cause was never
established. It is the defence-in-depth ask from the issue's "Ask" section:
"Whatever the cause: git-commit:@- should refuse a message whose first line
contains `paths = [` or `message = ` -- a leaked key is never a wanted
subject." Refusing on the SHAPE of the resulting subject catches this
regardless of which route produced it -- including a route that never
touches the TOML parser at all (the colon CLI, or a future payload form).
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
_spec = importlib.util.spec_from_file_location("git_commit_2656", _COMMIT_PATH)
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


def _run(args: list, cwd: Path, input_bytes: bytes = None) -> str:
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, timeout=120, cwd=str(cwd),
        env=dict(os.environ), input=input_bytes,
    )
    out = proc.stdout + proc.stderr
    return out.decode("utf-8", errors="replace")


def _head_subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


# --- unit level: the hazard predicate itself -------------------------------


def test_leaked_key_hazard_flags_paths_at_the_front_of_the_subject() -> None:
    marker = commit_mod._leaked_key_hazard(
        'paths = ["--all"] doctor: scaffold owned files\n\nbody text')
    assert marker == "paths = ["


def test_leaked_key_hazard_flags_message_key_too() -> None:
    marker = commit_mod._leaked_key_hazard('message = "fix: thing"')
    assert marker == "message = "


def test_leaked_key_hazard_is_none_for_an_ordinary_subject() -> None:
    """Positive control: a real subject and body, with neither marker
    anywhere, must not trip this."""
    msg = "fix: rename the paths helper\n\nbody line one\nbody line two"
    assert commit_mod._leaked_key_hazard(msg) is None


def test_leaked_key_hazard_only_looks_at_the_first_line() -> None:
    """A body paragraph that happens to mention `paths = [` further down
    is prose, not a leaked key -- only the subject line is load-bearing."""
    msg = "fix: rename\n\nthe old code read paths = [\"a\"] before this change"
    assert commit_mod._leaked_key_hazard(msg) is None


def test_leaked_key_hazard_only_fires_at_the_front_of_the_subject() -> None:
    """Self-review finding: the marker has to LEAD the subject, not merely
    appear in it -- an ordinary commit subject that legitimately discusses
    this op's own syntax (documenting it, adding the feature) must not be
    refused just because it mentions `paths = [` or `message = ` mid-sentence.
    A leaked key always prepends itself to the FRONT of the subject (the
    observed #2656 shape: `paths = [\"--all\"] doctor: scaffold ...`), so
    anchoring here is not a narrower guess -- it is the actual mechanism."""
    assert commit_mod._leaked_key_hazard(
        "feat: support paths = [] shorthand in the payload grammar") is None
    assert commit_mod._leaked_key_hazard(
        "docs: rename message = field to text = in payload spec") is None


# --- unit level: the refusal message itself --------------------------------


def test_leaked_key_refusal_appends_a_heredoc_marker_to_a_runnable_hint(
        monkeypatch) -> None:
    monkeypatch.setattr(commit_mod, "st_hint",
                        lambda arg: "./supertool " + repr(arg))
    lines = commit_mod._leaked_key_refusal("paths = [\"--all\"] x", "paths = [")
    joined = "\n".join(lines)
    assert "./supertool 'git-commit:@-' <<'EOF'" in joined
    assert joined.count("EOF") == 2  # the opener and the closer


def test_leaked_key_refusal_does_not_print_a_malformed_heredoc_when_no_supertool_is_found(
        monkeypatch) -> None:
    """Self-review finding: `st_hint`'s third state -- no runnable
    `./supertool` at all -- is a prose sentence in parentheses, not an
    invocation. Appending ` <<'EOF'` to it used to print a parenthesized
    error sentence followed by a heredoc redirect, which is not valid
    shell and would send a reader piping a TOML payload into what reads
    like a subshell wrapped around the error itself."""
    unrunnable = ("(no runnable supertool found in /nonexistent/dir -- "
                  "the op is 'git-commit:@-')")
    monkeypatch.setattr(commit_mod, "st_hint", lambda arg: unrunnable)
    lines = commit_mod._leaked_key_refusal("paths = [\"--all\"] x", "paths = [")
    joined = "\n".join(lines)
    assert unrunnable in joined
    assert "<<'EOF'" not in joined
    assert "EOF" not in joined.replace(unrunnable, "")


# --- end-to-end: the colon-CLI route ---------------------------------------


def test_leaked_paths_key_in_subject_is_refused_before_staging(
        tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    msg = 'paths = ["--all"] doctor: scaffold owned files'

    out = _run(["git-commit:::" + msg + ":::a.txt"], cwd=work)

    assert "ERROR" in out, out
    assert "2656" in out, out
    assert _head_subject(work) == "seed"


def test_leaked_message_key_in_subject_is_refused_before_staging(
        tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    msg = 'message = "fix: thing" tail'

    out = _run(["git-commit:::" + msg + ":::a.txt"], cwd=work)

    assert "ERROR" in out, out
    assert "2656" in out, out
    assert _head_subject(work) == "seed"


def test_ordinary_multiline_message_with_inline_path_is_unaffected(
        tmp_path: Path) -> None:
    """Positive control for the end-to-end path: a real multi-paragraph
    message with no leaked-key marker must commit exactly as it always
    has."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    msg = "subject line\n\nbody line one\nbody line two"

    out = _run(["git-commit:::" + msg + ":::a.txt"], cwd=work)

    assert "ERROR" not in out, out
    assert _head_subject(work) == "subject line"


# --- the payload route, proving the issue's own reproduction shape is ------
# --- already caught upstream by the TOML parser's own leftover detection ---


def test_the_issues_own_payload_shape_is_refused_by_the_toml_parser(
        tmp_path: Path) -> None:
    """Not this fix's own doing -- pinned here so a regression in either
    layer (the parser's leftover detection, #1830/#1868, or this refusal)
    is caught by this file. The issue's payload has an embedded ''' run in
    the message body (a quoted code fence), which the parser reads as an
    early close of the '''...''' block -- proving the parser refuses this
    shape rather than silently producing a leaked subject."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    payload = (
        "message = '''doctor: scaffold owned files, wire label "
        "vocabulary, arm supertool watch/radar\n\n"
        "body mentions a literal '''code fence''' inline.'''\n"
        'paths = ["--all"]\n'
    )
    out = _run(["git-commit:@-"], cwd=work,
               input_bytes=payload.encode("utf-8"))

    assert "ERROR" in out, out
    assert _head_subject(work) == "seed"
