Running `.github/scripts/coverage_gate.py` directly with no arguments does not
just read `ENFORCED`/`_source_lines()` -- it launches the entire pytest suite under
coverage (confirmed: output showed collection progressing through `........` percentage
markers up to 40% before a 300s timeout killed it). This is NOT the same thing as the
targeted guard tests (`tests/test_coverage_scope_861.py`,
`tests/test_coverage_gate_floor_991.py`) that merely import the module and call its
pure functions (`_source_lines()`, `classify()`, `_bucket_key()`) -- those are fast and
safe to run locally. Running the SCRIPT ITSELF (`python3 .github/scripts/coverage_gate.py`)
is effectively `pytest` plus coverage instrumentation over the whole tree, which is exactly
the "never run the full suite locally" rule CLAUDE.md states, just reached through a
script name that does not obviously say "runs the suite."

Cost: one wasted ~300s wall-clock wait (bounded only because I had set an explicit
`timeout 300` on the Bash call) attempting to "sanity check" the new coverage floor entry
by running the gate script directly, right after already having the answer from a properly
narrowed `pytest -n0 --cov=_supertool_catalog -k "..."` run.

Possible rule: when a developer lane wants to see the coverage-gate's own printed
verdict for an ENFORCED entry it just added, narrow with `-k` or a `--co`-only dry run
rather than invoking the bare script, or just trust the narrowed `--cov=MODULE` run's own
printed total% for that one module (it already prints a per-module table) instead of
reaching for the gate script's own CLI, which is written to be CI's full-tree entry point
and has no narrowed mode.
