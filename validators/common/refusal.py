"""Refusal-to-run detection shared by validator adapters (issue #406).

An analyser that declined to run — path outside its configured scope, no files
matched, no config to work from — has produced NO information about the file.
Reporting that as one error makes an unmeasured file indistinguishable from a
measured broken one, adds a `+1` to the before/after delta that no edit caused,
and can revert a good edit through `rollback_on_fail`.

Adapters route such exits through `skipped()` instead. See validators/SCHEMA.md,
"Skipped: the third state".
"""
from __future__ import annotations

import os

# Case-insensitive substrings by which an analyser announces it declined to run,
# as opposed to a finding about the file. Deliberately narrow: an exit we cannot
# explain must stay an error, because swallowing an unknown failure is the same
# category mistake in the other direction.
REFUSAL_PATTERNS = (
    "--paths allowlist",
    "outside the configured",
    "no files found to analyse",
    "no files found to analyze",
    "no files found to check",
)


def is_refusal(msg: str, env_var: str = "") -> bool:
    """Does `msg` read as the tool declining to run rather than a finding?

    `env_var` names an optional comma-separated list of extra substrings, so a
    repo can teach an adapter about a house-specific refusal without a release.
    """
    patterns = list(REFUSAL_PATTERNS)
    if env_var:
        patterns += [p.strip().lower()
                     for p in os.environ.get(env_var, "").split(",") if p.strip()]
    lowered = (msg or "").lower()
    return any(p in lowered for p in patterns)


def outside_roots(file_path: str, env_var: str) -> str | None:
    """Is `file_path` outside every analysis root named by `$env_var`? (#412)

    Returns a skip reason when the adapter can answer "not analysed" locally,
    and `None` whenever it cannot — which is the answer for an unset or blank
    var. That asymmetry is the whole design: an adapter cannot read the
    analyser's own scope (it lives in `phpstan.neon`, daemon flags, a config
    this module has no parser for and no business duplicating), so the two
    mistakes it could make are not equally bad.

    - Skipping a file that IS in scope loses the analysis silently. The file
      looks handled and is not. Unacceptable.
    - Analysing a file that is NOT in scope wastes a daemon round trip and
      still returns the right answer. Merely slow.

    So the scope is never inferred and never defaulted on. `$env_var` is an
    explicit statement by the repo that it knows the scope, and unset means
    "no local knowledge — the analyser decides", exactly as before.

    Roots are `os.pathsep`- or comma-separated, absolute or relative to the
    working directory. Matching is on `os.path.abspath` with an `os.sep`
    boundary: a bare prefix compare would read `/srcbad/Foo.php` as inside
    `/src`. Empty entries are dropped rather than abspath()ed into the working
    directory, where a stray separator would widen the allowlist to everything.

    The reason names `env_var`, not the analyser: a wrong skip is caused by
    this configuration, and pointing at a tool that never saw the file sends
    the reader to the wrong file to fix it.
    """
    raw = os.environ.get(env_var, "")
    if not raw.strip():
        return None
    entries = [e.strip() for part in raw.split(os.pathsep) for e in part.split(",")]
    roots = [os.path.normcase(os.path.abspath(e)) for e in entries if e]
    if not roots:
        return None
    target = os.path.normcase(os.path.abspath(file_path))
    for root in roots:
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return None
    return f"path outside {env_var} allowlist"


def skipped(tool: str, file_path: str, reason: str, dur_ms: int) -> dict:
    """A SCHEMA.md result in the third state: not clean, not broken, not looked at.

    The verdict keys `ok`, `count` and `errors` are **omitted**, not padded (#515).
    This helper padded them for a year while `docs/validators.md` told adapter
    authors to omit them, so a hand-rolled adapter and every helper caller
    published different shapes and nothing caught it.

    Omitting won because the argument for padding — "consumers can read
    `result['ok']` without a branch" — is not true of this codebase. Every core
    consumer tests `"skipped" in result` before touching a verdict key
    (`_validator_regressed`, `_validator_result_is_cacheable`,
    `_validator_render_row`, `_validator_render_diff`), and has to: the reason
    string exists only on a skip. The core's own built-in skips already omitted
    the keys, so padding here made the shared helper the odd one out. And an
    `ok: true` on a receipt that means "never looked at" is precisely the
    absence-read-as-a-pass failure the third state was introduced to end.

    `tool`, `file` and `duration_ms` stay: they describe the attempt, not a
    verdict about the file.
    """
    return {"tool": tool, "file": file_path, "duration_ms": dur_ms,
            "skipped": reason}
