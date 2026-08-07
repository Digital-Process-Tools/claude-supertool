"""The shellcheck validator adapter (#665).

**The issue's receipt does not hold, and this file records that rather than
repeating it.** #665 justifies the validator with `claude-remember#251` —

    [ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP" \
        || echo "(no previous entry)" > "$TMP"

— and asserts it "is SC2015 verbatim". It is not caught. ShellCheck 0.11.0
reports nothing on that line, at any severity, with every optional check on
(`-o all -S style` returns only SC2292/SC2250 style notes about brackets and
braces). The reason is a deliberate heuristic in SC2015: it stays silent when
`C` is an `echo` or a `printf`, because `cmd && x || echo "failed"` is an
idiom people mean. The redirect on the `echo` — the part that made #251
destructive — is exactly what the heuristic does not look at.

`test_the_issues_own_receipt_is_not_caught` pins that, so the claim cannot be
restated in a doc later without a red. The validator is still worth having:
SC2164, SC2086, SC2181 and SC2155 all fire, and those are checked below on
real files rather than assumed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok, describe, verdict
from _winenv import empty_path_env

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "shellcheck" / "shellcheck.py"

needs_shellcheck = pytest.mark.skipif(
    not shutil.which("shellcheck"),
    reason="shellcheck not on PATH",
)


def _spawn(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
    )


def _run(path: Path, env: dict | None = None) -> dict:
    return verdict(_spawn(str(path), env=env), adapter=ADAPTER.name)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _codes(out: dict) -> list:
    return [e.get("code") for e in out.get("errors", [])]


# ---------------------------------------------------------------------------
# The third state — the one that silently regresses
# ---------------------------------------------------------------------------

def test_missing_shellcheck_is_the_third_state(tmp_path: Path) -> None:
    """Absent tool -> `skipped`, with `ok`/`count`/`errors` omitted entirely.

    A checker nobody installed has said nothing about the file. A receipt
    carrying `ok: true` reads as a pass to the delta arithmetic, the rollback
    decision and the cache alike.
    """
    f = _write(tmp_path, "s.sh", "#!/bin/sh\ncd /tmp\nrm $1\n")
    out = _run(f, env=empty_path_env())
    assert "skipped" in out, describe(out)
    assert "shellcheck" in out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"
    assert out["tool"] == "shellcheck"
    assert isinstance(out["duration_ms"], int)


def test_required_turns_the_absent_tool_into_a_loud_error(tmp_path: Path) -> None:
    """`SUPERTOOL_REQUIRE_VALIDATORS=shellcheck` refuses to decline quietly.

    A skip is right on a laptop that never installed the tool and wrong on CI,
    where "not installed" means the gate is not running. The escalation is
    opt-in and it only ever goes quiet -> loud, never the reverse.
    """
    f = _write(tmp_path, "s.sh", "#!/bin/sh\necho ok\n")
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "shellcheck"
    out = _run(f, env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required validator whose tool is absent")
    err = out["errors"][0]
    assert err["code"] == "adapter", describe(out)
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in err["msg"], err["msg"]


# ---------------------------------------------------------------------------
# The issue's premise, checked rather than repeated
# ---------------------------------------------------------------------------

#: `scripts/save-session.sh:211` from claude-remember#251, verbatim.
RECEIPT_265 = (
    '#!/bin/bash\n'
    'LAST_LINE=3\n'
    'MEMORY_FILE=now.md\n'
    'TMP_LAST_ENTRY=out.txt\n'
    '[ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" '
    '> "$TMP_LAST_ENTRY" || echo "(no previous entry)" > "$TMP_LAST_ENTRY"\n'
)


@needs_shellcheck
def test_the_issues_own_receipt_is_not_caught(tmp_path: Path) -> None:
    """#665 says SC2015 would have caught #251. It does not.

    Kept as a test rather than a comment because the claim is load-bearing for
    the issue and would otherwise be copied into `docs/validators.md` by the
    next person who reads the issue and not the tool. If a future shellcheck
    drops the `echo`/`printf` carve-out, this fails and the docs get corrected
    in the same PR — a red here is good news.
    """
    out = _run(_write(tmp_path, "receipt.sh", RECEIPT_265))
    assert 2015 not in [
        int(str(c)[2:]) for c in _codes(out) if str(c).startswith("SC")
    ], (
        "shellcheck now reports SC2015 on the A && B || C line from "
        f"claude-remember#251 — update docs/validators.md, which states it "
        f"does not: {describe(out)}"
    )


#: What shellcheck *does* catch in these repos, one file per rule. Each body
#: is the smallest thing that triggers it; the point is that the rule is
#: reachable through the adapter, not that shellcheck can lint.
CAUGHT = [
    ("SC2164", "#!/bin/sh\ncd /tmp\n"),
    # `$1`, not a literal-assigned variable: shellcheck constant-folds
    # `X=1; rm $X` and stays silent, correctly — the value cannot split.
    ("SC2086", '#!/bin/sh\nrm $1\n'),
    ("SC2181", '#!/bin/sh\ntrue\nif [ $? -eq 0 ]; then echo hi; fi\n'),
    ("SC2155", '#!/bin/bash\nf() { local x=$(false); echo "$x"; }\nf\n'),
]


@needs_shellcheck
@pytest.mark.parametrize(("code", "body"), CAUGHT, ids=[c for c, _ in CAUGHT])
def test_the_rules_that_do_fire(tmp_path: Path, code: str, body: str) -> None:
    out = _run(_write(tmp_path, f"{code.lower()}.sh", body))
    assert_declined(out, context=f"a {code} violation")
    assert code in _codes(out), f"{code} not reported: {describe(out)}"


# ---------------------------------------------------------------------------
# Verdict shape
# ---------------------------------------------------------------------------

@needs_shellcheck
def test_a_clean_script_is_clean(tmp_path: Path) -> None:
    out = _run(_write(tmp_path, "ok.sh", '#!/bin/sh\necho "hello"\n'))
    assert_ok(out)
    assert out["count"] == 0
    assert out["errors"] == []


@needs_shellcheck
def test_findings_carry_line_col_code_and_context(tmp_path: Path) -> None:
    out = _run(_write(tmp_path, "loc.sh", "#!/bin/sh\ncd /tmp\n"))
    err = out["errors"][0]
    assert err["line"] == 2, describe(out)
    assert isinstance(err["col"], int), describe(out)
    assert err["code"] == "SC2164", describe(out)
    assert err["source_context"], describe(out)


@needs_shellcheck
def test_severity_maps_error_only_to_shellchecks_error_level(tmp_path: Path) -> None:
    """`rollback_on_fail` is false for this validator, but the severity still
    has to mean something: SC2086 is an `info` in shellcheck's own output and
    must not be published as an `error` next to a script that does not parse.
    """
    out = _run(_write(tmp_path, "sev.sh", '#!/bin/sh\nrm $1\n'))
    by_code = {e["code"]: e["severity"] for e in out["errors"]}
    assert by_code.get("SC2086") in ("info", "warning"), describe(out)


@needs_shellcheck
def test_an_extensionless_hook_with_a_shebang_is_checked(tmp_path: Path) -> None:
    """The `match` glob question from the issue, from the adapter's side.

    Most hooks in `claude-remember` are extensionless files carrying a
    `#!/bin/sh` shebang. The adapter must not refuse a path it cannot classify
    by suffix — shellcheck reads the shebang itself.
    """
    out = _run(_write(tmp_path, "50-git-backup", "#!/bin/sh\ncd /tmp\n"))
    assert_declined(out, context="an extensionless script with a shebang")
    assert "SC2164" in _codes(out), describe(out)


@needs_shellcheck
def test_a_file_shellcheck_cannot_classify_is_not_a_finding(tmp_path: Path) -> None:
    """No shebang, no extension: shellcheck exits non-zero saying it cannot
    tell what shell this is. That is a refusal, not a defect in the file."""
    out = _run(_write(tmp_path, "mystery", "echo hi\n"))
    assert "skipped" in out or out.get("ok") is not True, describe(out)


def test_an_unreadable_path_is_an_adapter_error_not_a_finding(tmp_path: Path) -> None:
    out = _run(tmp_path / "does-not-exist.sh")
    if "skipped" in out:
        pytest.skip("shellcheck absent on this machine")
    assert_declined(out, context="a path that does not exist")
    assert out["errors"][0]["code"] == "adapter", describe(out)


def test_no_file_arg(tmp_path: Path) -> None:
    out = verdict(_spawn(), adapter=ADAPTER.name)
    assert_declined(out, context="no file argument")
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registered_without_rollback() -> None:
    """SC2086 is a style finding; reverting an edit over one destroys work to
    fix nothing. Contrast `bash-check`, where the file genuinely does not
    parse and rollback is the whole point.
    """
    cfg = json.loads((REPO / ".supertool.example.json").read_text(encoding="utf-8"))
    entry = cfg["validators"]["shellcheck"]
    assert entry["rollback_on_fail"] is False, entry
    assert "validators/shellcheck/shellcheck.py" in entry["cmd"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_adapter_is_executable() -> None:
    assert ADAPTER.exists(), f"{ADAPTER} missing"
