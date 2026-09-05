"""#2146 -- gl-job declared one shared timeout for every mode. PR #2145
(#2105) raised it from 30s to 210s so a multi-id `:trace` fetch (up to
MAX_TRACE_IDS serially at up to 30s each) has enough budget. That raise was
correct for `:trace` and wrong for every other mode -- `:raw`, `:grep`,
`:fail`/`:errors` and the plain view inherited the same 210s ceiling and are
now caught up to 7x slower on a hang.

Fix (the narrowest of the three shapes the issue names): split the fan-out
into its own op, `gl-job-trace`, with its own timeout sized for
MAX_TRACE_IDS ids. `gl-job` itself drops back to a budget sized for its own
single-job metadata+trace(+MR) fetch, and REFUSES `:trace` mode directly
(rather than silently running it under a now-too-small budget) so a caller
who still types the old `gl-job:ID,ID:trace` form is told where the mode
moved instead of being caught by the very regression this issue is about.

The env flag `SUPERTOOL_TRACE_OP` is how `gl-job-trace`'s own dispatch tells
job.py's shared main() that trace mode is allowed on this call -- set by
gitlab.json's `trace_op` config key, which core exports automatically for
any non-reserved op-config key (see `_resolve_custom_op` in _supertool.py).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _PRESETS / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_job = _load("gitlab_job_2146", "gitlab/job.py")

_CONFIG = json.loads((_PRESETS / "gitlab.json").read_text(encoding="utf-8"))
_PER_ID_SECONDS = 10 + 20  # metadata timeout + trace timeout, per id, serial
# gl-job's own single-job worst case: metadata (10s) + trace (20s) + the
# MR lookup on an MR-sourced job (5s) -- job.py's default-view fetch.
_GL_JOB_SINGLE_FETCH_SECONDS = 10 + 20 + 5


def _op(op: str) -> dict:
    return _CONFIG["ops"][op]


def test_gl_job_timeout_did_not_inherit_the_trace_fan_out_budget() -> None:
    """gl-job's own declared timeout must cover its single-job fetch with
    margin, and must NOT still be sized for a MAX_TRACE_IDS serial fan-out --
    the #2146 regression. Asserting only the lower bound would still pass at
    210s (the pre-fix value), so the upper bound is the assertion that would
    actually fail if this fix did nothing.
    """
    timeout = _op("gl-job")["timeout"]
    assert timeout > _GL_JOB_SINGLE_FETCH_SECONDS, (
        f"gl-job timeout {timeout}s does not cover its own single-job fetch "
        f"({_GL_JOB_SINGLE_FETCH_SECONDS}s)")
    cap = gl_job.MAX_TRACE_IDS
    trace_worst_case = cap * _PER_ID_SECONDS
    assert timeout < trace_worst_case, (
        f"gl-job timeout {timeout}s covers a {cap}-id trace fan-out "
        f"({trace_worst_case}s) -- it has re-inherited the fan-out budget "
        f"this split exists to remove, so :raw/:grep/:fail/plain-view would "
        f"still be caught this much slower on a hang")


def test_gl_job_trace_op_covers_the_capped_id_count() -> None:
    cap = gl_job.MAX_TRACE_IDS
    worst_case = cap * _PER_ID_SECONDS
    timeout = _op("gl-job-trace")["timeout"]
    assert timeout > worst_case, (
        f"gl-job-trace timeout {timeout}s does not cover {cap} ids at "
        f"{_PER_ID_SECONDS}s each ({worst_case}s) -- a full multi-id fetch "
        f"at the cap would be killed before finishing")


def test_gl_job_trace_op_declares_the_env_flag_that_unlocks_trace_mode() -> None:
    """The split is enforced by gitlab.json's own declaration, not by a fact
    only this test file asserts."""
    entry = _op("gl-job-trace")
    assert entry.get("trace_op") is True, (
        "gl-job-trace must declare trace_op so core exports "
        "SUPERTOOL_TRACE_OP=true into its subprocess (see _resolve_custom_op)")


def test_gl_job_itself_also_declares_the_flag_as_false() -> None:
    """`gl-job` must declare `trace_op: false`, not merely omit the key.

    Auditor finding (self-review, #2146): core builds each op's subprocess
    env as `dict(os.environ)` -- the CALLER's ambient environment -- with
    only the declaring op's OWN config keys layered on top. An op that
    leaves a key undeclared never overwrites an ambient value of the same
    name, so if `gl-job`'s entry omitted `trace_op` entirely, an operator
    whose shell happened to already export `SUPERTOOL_TRACE_OP=true` (left
    over from some other command, or set by hand) would silently unlock
    `gl-job`'s own `:trace` dispatch -- the exact regression this split
    exists to close, reachable through ambient state rather than through
    the old syntax. Declaring `false` explicitly means core's own export
    loop always sets SUPERTOOL_TRACE_OP=\"false\" for a `gl-job` call,
    regardless of what the calling shell carried in.
    """
    entry = _op("gl-job")
    assert entry.get("trace_op") is False, (
        "gl-job must declare trace_op: false explicitly -- leaving the key "
        "absent lets an ambient SUPERTOOL_TRACE_OP=true in the caller's own "
        "shell silently unlock trace mode on the very op this split exists "
        "to keep it off of")


def test_gl_job_refuses_trace_mode_without_the_flag(monkeypatch, capsys) -> None:
    """Calling gl-job's own cmd path with mode=trace (the old syntax, or any
    other route that reaches job.py without the flag) must refuse rather than
    silently run under gl-job's now-smaller timeout budget -- the exact 'a
    mode nobody remembered to gate' failure this repo is named for.
    """
    monkeypatch.delenv("SUPERTOOL_TRACE_OP", raising=False)
    monkeypatch.setattr("sys.argv", ["job.py", "123", "trace"])
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "gl-job-trace" in out, out


def _fake_ensure(path: str):
    import os as _os
    _os.makedirs(path, exist_ok=True)
    return path, ""


def test_gl_job_allows_trace_mode_with_the_flag_set(monkeypatch, tmp_path, capsys) -> None:
    """Positive control for the refusal above: with the flag gl-job-trace's
    own config sets, the identical dispatch must still work."""
    monkeypatch.setenv("SUPERTOOL_TRACE_OP", "true")
    monkeypatch.setattr(
        gl_job, "_fetch_trace_and_meta",
        lambda job_id: (f"trace {job_id}\n", {"name": "j", "status": "failed"}, ""),
    )
    monkeypatch.setattr(gl_job._image_root, "default_root", lambda suffix: str(tmp_path))
    monkeypatch.setattr(gl_job._image_root, "ensure", _fake_ensure)
    monkeypatch.setattr("sys.argv", ["job.py", "123", "trace"])
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[trace]" in out, out
