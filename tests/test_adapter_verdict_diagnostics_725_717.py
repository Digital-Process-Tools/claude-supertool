"""A failing adapter assertion has to name the verdict it received (#725, #717).

Both issues are the same defect wearing two file names. `assert out["ok"] is
True` fires as `assert False is True` and discloses nothing: an adapter has
roughly a dozen paths to `ok=False` — tool missing, tool timed out, parse
error, no file arg, a decline the adapter invented — and that output separates
none of them. #658 spent one occurrence of `test_valid_ruby` and could not name
a cause. #725 spent one occurrence of the phplint spawn test and could not name
a cause either. Neither reproduces on demand, so each occurrence is the whole
budget for diagnosing it, and a bare boolean burns it.

#716 fixed this for one test by inlining the adapter's `errors` into the
message. This file turns that into something reusable, and tests the part #716
did not have to think about: **what the diagnostic does when it cannot read its
own input.** A message rendered from a payload the test does not own can meet a
shape it never anticipated — an adapter that crashed before emitting, a payload
that is a list, an `errors` entry that is a string. Rendering blank in those
cases would reproduce the original defect inside its own fix, which is this
repo's house failure exactly. So every case below asserts the message *says
what it could not read*.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _adapter_verdict as av  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
TESTS = REPO / "tests"


def _result(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["adapter"], returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# The happy path stays quiet and stays usable
# ---------------------------------------------------------------------------

def test_an_ok_verdict_passes_and_hands_back_the_payload() -> None:
    payload = {"tool": "phplint", "ok": True, "count": 0, "errors": []}
    assert av.assert_ok(payload) is payload


def test_parsing_a_clean_spawn_returns_the_payload() -> None:
    payload = {"tool": "phplint", "ok": True, "count": 0, "errors": []}
    assert av.verdict(_result(stdout=json.dumps(payload) + "\n")) == payload


# ---------------------------------------------------------------------------
# The failing path names the verdict — the whole point of both issues
# ---------------------------------------------------------------------------

def test_a_declining_verdict_names_the_adapters_own_reason() -> None:
    """The real thing, with a real adapter spawn and no external tool needed.

    `phplint.py` with no file argument declines with a stated reason and never
    reaches `php`, so this exercises the actual adapter contract on every
    runner including the ones that have no PHP at all.
    """
    r = subprocess.run(
        [sys.executable, str(VALIDATORS / "phplint" / "phplint.py"), ""],
        capture_output=True, text=True, timeout=60,
    )
    payload = av.verdict(r)
    assert payload["ok"] is False

    with pytest.raises(AssertionError) as excinfo:
        av.assert_ok(payload, context="a file with nothing wrong with it")
    msg = str(excinfo.value)
    assert "no file arg" in msg, f"the adapter's own msg is missing from: {msg}"
    assert "adapter" in msg, f"the error code is missing from: {msg}"
    assert "phplint" in msg, f"the tool name is missing from: {msg}"
    assert "a file with nothing wrong with it" in msg


def test_the_message_carries_the_duration_so_a_wall_hit_is_visible() -> None:
    """A decline at 30000ms is the adapter's own budget firing; at 4ms it is not.

    That is precisely the question #725 could not answer, and the adapter
    reports it in every payload it emits.
    """
    fast = av.describe({"tool": "phplint", "ok": False, "count": 1, "duration_ms": 4,
                        "errors": [{"code": "adapter", "msg": "php binary not found"}]})
    wall = av.describe({"tool": "phplint", "ok": False, "count": 1, "duration_ms": 30000,
                        "errors": [{"code": "adapter", "msg": "timeout"}]})
    assert "4ms" in fast
    assert "30000ms" in wall
    assert av.describe({"tool": "t", "ok": False, "errors": []}).strip()


def test_the_message_carries_every_field_of_a_structured_error() -> None:
    payload = {
        "tool": "ruby-check", "ok": False, "count": 1,
        "errors": [{"line": 12, "col": 3, "severity": "error",
                    "code": "syntax", "msg": "unexpected end-of-input"}],
    }
    msg = av.describe(payload)
    for needle in ("12", "syntax", "unexpected end-of-input"):
        assert needle in msg, f"{needle!r} missing from {msg!r}"


def test_many_errors_are_capped_and_the_message_says_how_many_it_hid() -> None:
    payload = {
        "tool": "eslint", "ok": False, "count": 9,
        "errors": [{"line": n, "code": "e", "msg": f"problem {n}"} for n in range(9)],
    }
    msg = av.describe(payload)
    assert "problem 0" in msg
    assert "problem 8" not in msg, "an uncapped dump buries the first error it should be showing"
    assert "9" in msg and "more" in msg, f"a capped list has to say what it hid: {msg!r}"


def test_an_adapter_that_should_have_declined_and_did_not_says_what_it_returned() -> None:
    payload = {"tool": "phplint", "ok": True, "count": 0, "errors": []}
    assert av.assert_declined(dict(payload, ok=False, count=1, errors=[])) is not None

    with pytest.raises(AssertionError) as excinfo:
        av.assert_declined(payload, context="a file with a deliberate syntax error")
    msg = str(excinfo.value)
    assert "a file with a deliberate syntax error" in msg
    assert "phplint" in msg and "count=0" in msg


# ---------------------------------------------------------------------------
# The diagnostic cannot read its input — it says so, it does not render blank
# ---------------------------------------------------------------------------

def test_an_empty_stdout_says_so_instead_of_raising_JSONDecodeError() -> None:
    with pytest.raises(AssertionError) as excinfo:
        av.verdict(_result(stdout="   \n", stderr="Traceback (most recent call last)", rc=1))
    msg = str(excinfo.value)
    assert "no output" in msg.lower() or "empty" in msg.lower()
    assert "Traceback" in msg, "stderr is the only evidence left when stdout is empty"
    assert "1" in msg, "the exit code is evidence too"


def test_unparseable_stdout_shows_what_it_could_not_parse() -> None:
    with pytest.raises(AssertionError) as excinfo:
        av.verdict(_result(stdout="Fatal error: something\n", rc=255))
    msg = str(excinfo.value)
    assert "Fatal error: something" in msg
    assert "255" in msg


def test_a_payload_that_is_not_an_object_says_what_it_was() -> None:
    with pytest.raises(AssertionError) as excinfo:
        av.verdict(_result(stdout="[1, 2, 3]\n"))
    assert "list" in str(excinfo.value)


def test_a_payload_with_no_ok_key_names_the_absence_and_lists_what_was_there() -> None:
    with pytest.raises(AssertionError) as excinfo:
        av.assert_ok({"tool": "phplint", "count": 0})
    msg = str(excinfo.value)
    assert '"ok"' in msg or "'ok'" in msg
    assert "tool" in msg and "count" in msg, f"the keys it did find are the lead: {msg!r}"


def test_a_declining_payload_with_no_errors_key_does_not_render_blank() -> None:
    """The house defect in miniature: a diagnostic with nothing to say must say that."""
    msg = av.describe({"tool": "phplint", "ok": False, "count": 1})
    assert msg.strip(), "a blank diagnostic is the bug this file exists to prevent"
    assert "errors" in msg
    assert "tool" in msg and "count" in msg


def test_an_errors_value_of_the_wrong_type_still_renders() -> None:
    for wrong in ("boom", 42, {"code": "x"}, None):
        msg = av.describe({"tool": "t", "ok": False, "errors": wrong})
        assert msg.strip()
        assert "boom" in msg or "42" in msg or "x" in msg or "None" in msg, msg


def test_an_error_entry_that_is_not_a_mapping_still_renders() -> None:
    msg = av.describe({"tool": "t", "ok": False, "errors": ["plain string problem", 7]})
    assert "plain string problem" in msg
    assert "7" in msg


def test_describe_of_something_that_is_not_a_payload_at_all_says_so() -> None:
    for junk in (None, [], "ok", 3):
        msg = av.describe(junk)
        assert msg.strip(), f"describe({junk!r}) rendered blank"
        assert "payload" in msg.lower() or "object" in msg.lower(), msg


def test_the_message_is_bounded_so_a_huge_payload_cannot_swamp_the_report() -> None:
    payload = {"tool": "t", "ok": False, "errors": [{"code": "c", "msg": "x" * 20_000}]}
    msg = av.describe(payload)
    assert len(msg) < 4_000, f"an unbounded dump is not a diagnostic ({len(msg)} chars)"
    assert "x" in msg


# ---------------------------------------------------------------------------
# The recurrence guard — #725's actual complaint
# ---------------------------------------------------------------------------

def _bare_ok_assertions(path: Path) -> list[int]:
    """Lines asserting `<expr>["ok"] is <bool>` with no message attached."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or node.msg is not None:
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Is):
            continue
        if not isinstance(test.comparators[0], ast.Constant) or not isinstance(
            test.comparators[0].value, bool
        ):
            continue
        left = test.left
        if (
            isinstance(left, ast.Subscript)
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == "ok"
        ):
            found.append(node.lineno)
    return found


def test_a_file_that_adopts_the_convention_adopts_it_everywhere() -> None:
    """#725 is not "a flaky test" — it is a lesson applied to one line and not the next.

    #716 replaced `assert out["ok"] is True` in `test_valid_ruby` with an
    assertion carrying the adapter's errors, and in the same PR added a new
    `assert ...["ok"] is True` in a new file. Nobody noticed, because nothing
    was watching. This watches: once a test file imports the shared helper, no
    bare `["ok"] is <bool>` may remain in it. It deliberately does not police
    files that have not adopted it — the remaining sites are enumerated in the
    PR and tracked separately, and a guard that fails on work nobody has done
    yet is a guard that gets deleted.
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "_adapter_verdict" not in text:
            continue
        if path.name == Path(__file__).name:
            continue
        lines = _bare_ok_assertions(path)
        if lines:
            offenders[path.name] = lines
    assert not offenders, (
        "these files use the shared adapter-verdict helper but still assert a "
        f"verdict as a bare boolean somewhere: {offenders}"
    )


def test_the_guard_can_actually_see_an_offender(tmp_path: Path) -> None:
    """A guard that has never caught anything has not been shown to work."""
    subject = tmp_path / "test_sample.py"
    subject.write_text(
        "def test_x():\n"
        "    assert out['ok'] is True\n"
        "    assert out['ok'] is False\n"
        "    assert out['ok'] is True, 'named'\n"
        "    assert other['count'] is True\n",
        encoding="utf-8",
    )
    assert _bare_ok_assertions(subject) == [2, 3]
