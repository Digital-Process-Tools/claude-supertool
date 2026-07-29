"""A warm-process validator declines on targets it cannot judge (#345).

BACKGROUND. `phpunit-mcp` reported 2 failures on a DVSI test extending
`SiControllerTestCase`; the cold `phpunit:` op on the same file, same commit,
same `phpunit.xml`, passed 3/3 with 173 assertions. The reds were *fabricated*,
not pre-existing: `mcp-phpunit-warm` runs the project's phpunit.xml bootstrap in
the long-lived PARENT (prewarm, on by default) and forks a child per call, so
whatever that bootstrap opened — a DB handle, a session, a platform singleton —
is shared across every child and across the parent. The failure therefore
depends on warm-process state, not on the file, which is why a cold run cannot
reproduce it. Same family as #265 (phpunit staleness) and #273 (rector
ClassReflection).

The framework's regression-only rollback (#406) already refuses to roll an edit
back for these, because the pre-edit baseline pass fabricates them too. What it
does NOT do is stop them rendering as failures of the file. Suppressing them as
"pre-existing" would be wrong in the other direction: a pre-existing failure is
a real failure, and hiding it is how a broken file starts looking clean.

So the fix is #482's shape — a tool that cannot answer must say so rather than
guess. `validators.<name>.warm_unsafe` is a list of regexes matched against the
resolved target's content; a hit makes the validator return `skipped` (an
absence of information) instead of running an adapter whose verdict cannot be
trusted for that target.

Both directions are pinned here:
  * a target the config marks warm-unsafe never yields a red, even when the
    adapter would have emitted one (the fabricated case);
  * a target it does not mark still yields the adapter's red in full (a genuine
    failure must survive).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import supertool


def _cmd(payload: dict) -> str:
    """Cross-platform cmd printing `payload` as JSON (mirrors test_validators)."""
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return (
        f'{{python}} -c "import sys, base64; '
        f"sys.stdout.write(base64.b64decode('{encoded}').decode())"
        f'"'
    )


_RED = {
    "tool": "phpunit-mcp", "file": "x.php", "ok": False, "count": 2,
    "errors": [
        {"line": 42, "col": None, "severity": "error", "code": "phpunit.failure",
         "msg": "testList: Looking for Table in response <div ...>"},
        {"line": 58, "col": None, "severity": "error", "code": "phpunit.failure",
         "msg": "testFilter: Looking for Table in response <div ...>"},
    ],
    "duration_ms": 900,
}

_DB_TEST = (
    "<?php\n"
    "namespace Dvsi\\Tests;\n"
    "final class ServiceListDelegateTest extends SiControllerTestCase\n"
    "{\n"
    "    public function testList(): void {}\n"
    "}\n"
)

_PLAIN_TEST = (
    "<?php\n"
    "namespace Dvsi\\Tests;\n"
    "final class CommandGetThingTest extends SiTestCase\n"
    "{\n"
    "    public function testRun(): void {}\n"
    "}\n"
)


def _spec(cmd: str, **extra: object) -> dict:
    spec: dict = {"cmd": cmd, "timeout": 10, "cache": False}
    spec.update(extra)
    return spec


# ---------------------------------------------------------------------------
# Direction 1 — a fabricated red must not be reported as a failure
# ---------------------------------------------------------------------------

def test_warm_unsafe_target_is_skipped_not_failed(tmp_path: Path) -> None:
    """The adapter would have said `2 err`; the framework declines instead."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out, out
    assert "ok" not in out or out.get("ok") is not False
    assert out.get("count", 0) == 0
    assert not out.get("errors")


def test_skip_reason_names_the_pattern_that_matched(tmp_path: Path) -> None:
    """`skipped` alone sends the reader back to the config to guess why (#406)."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    reason = str(out["skipped"])
    assert "warm-unsafe" in reason
    assert "SiControllerTestCase" in reason


def test_warm_unsafe_skip_never_regresses_and_never_rolls_back(tmp_path: Path) -> None:
    """A decline is an absence of information — it can never mark ✗."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    green_before = {"tool": "phpunit-mcp", "ok": True, "count": 0}
    assert supertool._validator_regressed(green_before, out) is False
    assert supertool._validator_regressed(None, out) is False


def test_warm_unsafe_skip_renders_as_skipped_row(tmp_path: Path) -> None:
    """What the caller reads must not look like a finding about the file."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    row = "\n".join(supertool._validator_render_row(out))
    assert "skipped" in row
    assert "err" not in row
    assert "Looking for Table" not in row


def test_warm_unsafe_declines_before_the_adapter_runs(tmp_path: Path) -> None:
    """No daemon call, no cost — the decline is decided from the target alone."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    marker = tmp_path / "adapter-ran"
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('yes')\n"
        f"sys.stdout.write({json.dumps(json.dumps(_RED))})\n",
        encoding="utf-8",
    )
    spec = _spec(f"{{python}} {adapter.as_posix()} {{file}}",
                 warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out
    assert not marker.exists(), "adapter was invoked despite the warm-unsafe gate"


# ---------------------------------------------------------------------------
# Direction 2 — a genuine failure must still be reported
# ---------------------------------------------------------------------------

def test_non_matching_target_still_reports_the_failure(tmp_path: Path) -> None:
    """The gate is targeted, not a blanket mute on the validator."""
    f = tmp_path / "CommandGetThingTest.php"
    f.write_text(_PLAIN_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" not in out, out
    assert out["ok"] is False
    assert out["count"] == 2
    assert supertool._validator_regressed({"ok": True, "count": 0}, out) is True


def test_no_warm_unsafe_key_leaves_behaviour_unchanged(tmp_path: Path) -> None:
    """Opt-in: a spec without the key behaves exactly as before."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    out = supertool._validator_run_one("phpunit", _spec(_cmd(_RED)), str(f))
    assert "skipped" not in out
    assert out["count"] == 2


def test_empty_warm_unsafe_list_leaves_behaviour_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    out = supertool._validator_run_one("phpunit", _spec(_cmd(_RED), warm_unsafe=[]), str(f))
    assert "skipped" not in out
    assert out["count"] == 2


def test_matching_target_that_passes_is_also_declined(tmp_path: Path) -> None:
    """A green from an untrustworthy runner is worth no more than its red."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    green = {"tool": "phpunit-mcp", "file": "x.php", "ok": True, "count": 0,
             "errors": [], "duration_ms": 5}
    spec = _spec(_cmd(green), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out


# ---------------------------------------------------------------------------
# Config robustness — a bad pattern must not take the validator down
# ---------------------------------------------------------------------------

def test_bare_string_is_accepted_as_a_single_pattern(tmp_path: Path) -> None:
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=r"extends\s+SiControllerTestCase")
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out


def test_invalid_regex_is_ignored_and_the_validator_still_runs(tmp_path: Path) -> None:
    """A typo in config must not silently mute — nor crash — the validator."""
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED), warm_unsafe=["extends ([unclosed"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" not in out
    assert out["count"] == 2


def test_one_bad_pattern_does_not_disarm_a_good_one(tmp_path: Path) -> None:
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    spec = _spec(_cmd(_RED),
                 warm_unsafe=["([unclosed", r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out


def test_non_list_non_string_warm_unsafe_is_ignored(tmp_path: Path) -> None:
    f = tmp_path / "ServiceListDelegateTest.php"
    f.write_text(_DB_TEST, encoding="utf-8")
    out = supertool._validator_run_one("phpunit", _spec(_cmd(_RED), warm_unsafe=7), str(f))
    assert "skipped" not in out
    assert out["count"] == 2


def test_unreadable_target_does_not_trigger_a_decline(tmp_path: Path) -> None:
    """Cannot evaluate the gate → leave the pre-#345 behaviour in place rather
    than mute a validator on every file the gate could not read."""
    missing = tmp_path / "gone" / "NoSuchTest.php"
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(missing))
    assert "skipped" not in out
    assert out["count"] == 2


def test_binary_target_does_not_crash_the_gate(tmp_path: Path) -> None:
    f = tmp_path / "blob.php"
    f.write_bytes(b"<?php \xff\xfe extends SiControllerTestCase \x00")
    spec = _spec(_cmd(_RED), warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(f))
    assert "skipped" in out


# ---------------------------------------------------------------------------
# The gate reads the RESOLVED target, not the edited file
# ---------------------------------------------------------------------------

def test_gate_matches_the_resolved_target_not_the_edited_file(tmp_path: Path) -> None:
    """`validators.phpunit.resolve` maps a source file to its test file; the
    warm-unsafe decision belongs to whatever actually gets run."""
    src = tmp_path / "ServiceListDelegate.php"
    src.write_text("<?php\nfinal class ServiceListDelegate {}\n", encoding="utf-8")
    test = tmp_path / "ServiceListDelegateTest.php"
    test.write_text(_DB_TEST, encoding="utf-8")
    resolver = tmp_path / "resolve.py"
    resolver.write_text(f"import sys\nsys.stdout.write({str(test)!r})\n", encoding="utf-8")
    spec = _spec(_cmd(_RED),
                 resolve=f"{{python}} {resolver.as_posix()} {{file}}",
                 warm_unsafe=[r"extends\s+SiControllerTestCase"])
    out = supertool._validator_run_one("phpunit", spec, str(src))
    assert "skipped" in out
    assert "SiControllerTestCase" in str(out["skipped"])


# ---------------------------------------------------------------------------
# A decline is never frozen into the cache
# ---------------------------------------------------------------------------

def test_warm_unsafe_skip_is_not_cacheable() -> None:
    """The key is a content hash; config decides the skip. Freezing one keeps
    skipping a file that config later brings back into scope (#406)."""
    data = {"tool": "phpunit", "skipped": "warm-unsafe: ..."}
    assert supertool._validator_result_is_cacheable(data) is False
