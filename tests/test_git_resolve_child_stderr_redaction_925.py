"""A failed validator's stderr reaches the resolve receipt unredacted (#925).

`_child_failed` puts the first line of a dead validate child's stderr into the
receipt. Validator adapters shell out -- `phpstan`, `tsc-check`, `xmllint` --
and a child dying on an auth error puts the credential it tried on stderr: a
`user:token@host` URL, an `Authorization: Bearer ...`, a `GITLAB_TOKEN=...`
echoed by a wrapper. `presets/_secrets.redact` existed and was wired only into
`claude-log` / `devto` / `bluesky`, so this boundary bypassed it.

`_skip_summary` is the same class one function away: the `(why)` of a declined
validator is also the adapter's own text, and it lands on the same line.

Would these pass if the code did nothing? No -- each one feeds a credential
through the real function and asserts the credential is absent *and* that the
redaction marker is present. Absence alone also holds when the function
returns "".
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))

import _secrets  # noqa: E402

PRESET = PRESETS / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_925", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

#: Shaped like a real GitLab PAT so `_secrets` recognises it; not one.
FAKE_PAT = "glpat-0123456789abcdefghij"
FAKE_KEY = "sk-ant-0123456789abcdefghijklmnop"


def _failed(stderr: str, returncode: int = 1) -> str:
    return resolve._child_failed(subprocess.CompletedProcess(
        args=["validate"], returncode=returncode, stdout="", stderr=stderr))


def test_a_token_in_a_url_does_not_reach_the_receipt() -> None:
    cell = _failed("fatal: could not read from https://ci:" + FAKE_PAT + "@gitlab.example/x")

    assert FAKE_PAT not in cell, cell
    assert _secrets.MARKER_PREFIX in cell, (
        "the value is gone but nothing says so -- an empty cell is the other "
        "way that assertion passes: " + cell)


def test_a_bearer_header_echoed_by_a_dying_adapter_is_redacted() -> None:
    cell = _failed("curl -H 'Authorization: Bearer " + FAKE_KEY + "' failed")

    assert FAKE_KEY not in cell, cell
    assert _secrets.MARKER_PREFIX in cell, cell


def test_the_exit_status_survives_the_redaction() -> None:
    """Redacting must not cost the cell the fact it exists to carry."""
    cell = _failed("boom " + FAKE_PAT, returncode=-9)

    assert "killed by signal 9" in cell, cell
    assert FAKE_PAT not in cell, cell


def test_truncation_cannot_leave_a_prefix_of_the_secret() -> None:
    """Redaction has to run before the 120-char cap, not after.

    This padding puts the cut *inside* the token, leaving 12 characters after
    `glpat-` where the rule wants 16 -- so a redaction that runs on the already
    truncated cell matches nothing and the head of the credential ships. Order
    is the whole assertion; both orders pass every other test in this file.
    """
    cell = _failed("x" * 100 + " " + FAKE_PAT)

    assert FAKE_PAT[:18] not in cell, cell


def test_a_declined_validators_reason_is_redacted_too() -> None:
    """Same line, same class, one function away."""
    cell = resolve._skip_summary([("phpstan", "auth failed for https://ci:" + FAKE_PAT + "@h/x")])

    assert FAKE_PAT not in cell, cell
    assert _secrets.MARKER_PREFIX in cell, cell
    assert "phpstan" in cell, "the redaction ate the tool name: " + cell


def test_ordinary_stderr_is_untouched() -> None:
    """A false positive here costs the receipt its only diagnostic."""
    cell = _failed("SyntaxError: invalid syntax (foo.py, line 12)")

    assert "SyntaxError: invalid syntax (foo.py, line 12)" in cell, cell
    assert _secrets.MARKER_PREFIX not in cell, cell

def test_a_declined_validators_reason_cannot_rewrite_the_line() -> None:
    """The decline reason is a child's text on a line the tool owns.

    `_child_failed` has gone through `_untrusted.flat` since #883; this sibling
    cell, built from the adapter's own reason, did not -- so a carriage return
    in it overwrote the receipt row it is embedded in. Same class, one function
    away, and on the line this issue was already changing.
    """
    cell = resolve._skip_summary([("phpstan", "boom\rmarkers: clean")])

    assert "\r" not in cell, repr(cell)
    assert "\n" not in cell, repr(cell)
    assert "phpstan" in cell, cell
