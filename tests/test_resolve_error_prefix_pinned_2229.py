"""The `RESOLVE-ERROR: ` protocol was two separately-typed string literals
with no assertion binding them (#2229):

- producer: `validators/common/ci_lint_resolve_root.py:48`
  `RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "`
- consumer: `_supertool.py` `_VALIDATOR_RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "`

`_validator_run_one` (core, one process, the caller of every adapter) cannot
import the producer: adapters run as a subprocess with only
`validators/common` on `sys.path`, and `ci_lint_resolve_root.py` cannot import
the core either -- reaching across that boundary is exactly the "gate stops
working whenever a sys.path resolution does" trade `_validator_required`
(`_supertool.py`, `tests/test_require_validators_core_975.py`) and
`_LINE_BREAK_PATTERN`
(`tests/test_validators_splitlines_1486.py::test_split_lines_matches_the_core_conservative_definition`)
already declined for the same reason -- and `_ANSI_ESCAPE_RE`/`ANSI_RE`
(`tests/test_tsc_check.py::test_the_adapter_and_the_core_strip_the_same_escapes`)
is the same trade again, a third time, for a third pair of literals.

So the fix here is the same shape as those: keep the two
literals, and pin them equal with a test that loads both real modules rather
than trusting the two authors to keep retyping the same nine characters
correctly forever.

If the producer's spelling ever drifts, `test_the_two_literals_are_pinned_equal`
below goes red before anything downstream does. Without it, the drift's real
symptom is silent and wrong in the dangerous direction: `_validator_resolve`
no longer recognises the sentinel, the skip arm in `_validator_run_one` is
never taken, and the sentinel sentence gets treated as a resolved path --
substituted into the adapter's `{file}` and `shlex.split`, then handed to the
callee's option parser as several argv words (`_supertool.py:23521`,
`_shield_substitute`).

`test_the_real_resolver_emitting_it_is_recognised_end_to_end` below is the
second piece #2229 asked for: the missing positive control demonstrating the
whole path with nothing mocked -- the real `ci_lint_resolve_root.py` resolver
subprocess, invoked by the real `_validator_resolve`/`_validator_run_one`,
against a real non-git directory that forces it to emit the sentinel, asserting
the skip arm is the one taken. A near-identical drive already existed at
`tests/test_ci_lint_resolve_2174_2177.py::test_a_resolve_error_reaches_the_final_skip_dict_with_its_reason`
(added for #2177) and, checked by hand here, already goes red under the same
deliberately-broken-prefix probe this file's own TDD proof used -- so the gap
#2229 actually names is the missing *pin*, not a missing behavioural drive.
This test is kept anyway, colocated with the pin, so a reader auditing "is the
sentinel bound to something" finds both answers in one file rather than having
to already know the older file exists.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supertool  # noqa: E402
from _adapter_budget import adapter_budget  # noqa: E402
from _adapter_verdict import skip_if_core_timed_out  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RESOLVER = REPO / "validators" / "common" / "ci_lint_resolve_root.py"


def _load_resolver_module():
    # The resolver's own `from refusal import guard_main` needs
    # `validators/common` on sys.path -- it runs as a subprocess normally,
    # where its own directory is argv[0]'s directory, but importlib here
    # loads it by file path alone and never sets that up on its own.
    sys.path.insert(0, str(RESOLVER.parent))
    spec = importlib.util.spec_from_file_location(
        "_ci_lint_resolve_root_2229", RESOLVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_two_literals_are_pinned_equal() -> None:
    """MUST FIRE (were the two spellings ever to differ): the whole point of
    this file. Loads the real producer module rather than hardcoding its
    value a third time, which would just be a fourth untethered literal."""
    mod = _load_resolver_module()
    assert mod.RESOLVE_ERROR_PREFIX == supertool._VALIDATOR_RESOLVE_ERROR_PREFIX, (
        "the producer's RESOLVE_ERROR_PREFIX and the consumer's "
        "_VALIDATOR_RESOLVE_ERROR_PREFIX have drifted apart: "
        + repr((mod.RESOLVE_ERROR_PREFIX, supertool._VALIDATOR_RESOLVE_ERROR_PREFIX)))


def test_the_real_resolver_emitting_it_is_recognised_end_to_end(tmp_path: Path) -> None:
    """MUST FIRE: the real resolver, run as a real subprocess against a real
    non-git directory, emits the sentinel on stdout; fed through the real
    `_validator_run_one`, the skip arm must be the one taken."""
    mod = _load_resolver_module()
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    target = not_a_repo / "README.md"
    target.write_text("hi\n")

    r = subprocess.run(
        [sys.executable, str(RESOLVER), str(target)],
        capture_output=True, text=True, timeout=adapter_budget(RESOLVER),
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    stdout = r.stdout.strip()
    assert stdout.startswith(mod.RESOLVE_ERROR_PREFIX), (
        "fixture assumption broken: a non-git directory should make the real "
        "resolver emit its own sentinel: " + repr(stdout))

    resolver_slashed = str(RESOLVER).replace("\\", "/")
    spec = {"resolve": "{python} " + resolver_slashed + " {file}"}
    result = skip_if_core_timed_out(
        supertool._validator_run_one("ci-lint", spec, str(target)))
    assert result is not None
    assert "skipped" in result, (
        "the real sentinel must render as a skip, not vanish or be treated "
        "as a resolved path: " + repr(result))
    assert "not inside a git repository" in result["skipped"], (
        "the real resolver's own reason must survive to the final skip "
        "message: " + repr(result))
